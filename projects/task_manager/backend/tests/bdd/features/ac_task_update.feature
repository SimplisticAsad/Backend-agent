# ac.task.update  (requirements: requirement.task_management; operations: operation.task.update)
Feature: Update Task succeeds

  Scenario: Update Task succeeds
    Given the user is signed in as Manager
    And the record being worked on exists
    When Change the Task details
    And Update Task
    Then The changes to the Task are saved
