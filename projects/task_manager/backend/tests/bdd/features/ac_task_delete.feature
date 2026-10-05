# ac.task.delete  (requirements: requirement.task_management; operations: operation.task.delete)
Feature: Delete Task succeeds

  Scenario: Delete Task succeeds
    Given the user is signed in as Manager
    And the record being worked on exists
    When Confirm deleting the Task
    And Delete Task
    Then The Task is removed from the Task list
