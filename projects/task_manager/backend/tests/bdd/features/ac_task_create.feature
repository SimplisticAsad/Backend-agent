# ac.task.create  (requirements: requirement.task_management; operations: operation.task.create)
Feature: Create Task succeeds

  Scenario: Create Task succeeds
    Given the user is signed in as Manager
    When Fill in the Task form
    And Create Task
    Then The Task is created and appears in the Task list
